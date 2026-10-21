import SwiftUI

@main
struct MacMCPMenuApp: App {
    @NSApplicationDelegateAdaptor(MenuAppDelegate.self) private var appDelegate
    @StateObject private var state: AppState

    init() {
        let appState = AppState()
        _state = StateObject(wrappedValue: appState)
        appState.presentTour = { [weak appState] in
            guard let appState else { return }
            OnboardingWindowController.shared.show(state: appState)
        }
        appState.presentUpdate = { [weak appState] in
            guard let appState else { return }
            SettingsWindowController.shared.show(state: appState, settings: appState.settings, section: .general)
        }
        appState.presentFleetRequests = { [weak appState] in
            guard let appState else { return }
            SettingsWindowController.shared.show(state: appState, settings: appState.settings, section: .macs)
        }
        // The menu bar app opens at login while start at login is on.
        appState.syncLoginItem = { enabled in LoginItem.sync(enabled: enabled) }
        LoginItem.sync(enabled: appState.settings.startAtLogin)
        LiveIslandController.shared.attach(appState)
        MenuAppDelegate.openSettings = { [weak appState] pane in
            let aliases: [String: SettingsSection] = ["scheduled-tasks": .scheduled, "automation": .scheduled,
                                                      "recipes": .scheduled, "mcp-servers": .mcpServers,
                                                      "fleet": .macs, "devices": .macs, "iphone": .macs]
            guard let appState,
                  let section = aliases[pane] ?? SettingsTabs.section(forLink: pane) ?? SettingsSection(rawValue: pane)
            else { return }
            // Automation holds Scheduled Tasks and Recipes; a link names the tab it wants.
            if section == .scheduled { AutomationNavigation.shared.tab = pane == "recipes" ? .recipes : .tasks }
            // Tabbed panes keep the tab last shown unless a link names one (macmcp://settings/skills).
            SettingsTabs.shared.select(link: pane)
            SettingsWindowController.shared.show(state: appState, settings: appState.settings, section: section)
        }
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
            Image(nsImage: MenuBarMark.image(menuBarLook))
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

    private var menuBarLook: MenuBarMark.Look {
        if !state.serverRunning { return .stopped }
        if state.activeAgents > 0 || (state.hasReliableSessionSignal && state.sessionActiveCount > 0) {
            // The dot stays filled without the pulse (Reduce Motion), so work still reads at a glance.
            return .working(hollow: state.pulse)
        }
        return .idle
    }

    private var menuBarAccessibilityLabel: String {
        // Same words as the popover header and Settings (shared StatusDisplay mapping).
        if state.startingAfterLogin { return "Mac MCP, starting" }
        if !state.serverRunning { return "Mac MCP, \(state.connectionState.display.label.lowercased())" }
        if state.activeAgents > 0 || (state.hasReliableSessionSignal && state.sessionActiveCount > 0) {
            return "Mac MCP, active work"
        }
        if state.connectionState == .degraded { return "Mac MCP, degraded" }
        return "Mac MCP"
    }
}

/// The Mac MCP artwork bundled in Resources/Assets (rendered by scripts/build_brand.py).
enum BrandArt {
    static func image(_ name: String) -> NSImage {
        Bundle.main.url(forResource: name, withExtension: "png", subdirectory: "Assets")
            .flatMap(NSImage.init(contentsOf:)) ?? NSApp.applicationIconImage
    }
}

/// The monogram in the menu bar. It is a template image, so macOS colours it for light, dark
/// and highlighted bars; the state shows in alpha and a dot, never in colour.
@MainActor
enum MenuBarMark {
    enum Look: Hashable {
        case stopped, idle
        case working(hollow: Bool)
    }

    /// Every look is the same width, so the other menu bar icons never move when work starts or stops.
    private static let size = NSSize(width: 22, height: 18)

    /// The 18 pt mark with its @1x and @2x pixels, so it stays sharp on every display.
    private static let base: NSImage = {
        let markSize = NSSize(width: 18, height: 18)
        let image = NSImage(size: markSize)
        for name in ["MenuBarMark", "MenuBarMark@2x"] {
            guard let url = Bundle.main.url(forResource: name, withExtension: "png", subdirectory: "Assets"),
                  let rep = NSImageRep(contentsOf: url) else { continue }
            rep.size = markSize
            image.addRepresentation(rep)
        }
        // A bundle without the art still shows something to click.
        if image.representations.isEmpty, let symbol = NSImage(systemSymbolName: "cursorarrow", accessibilityDescription: nil) {
            return symbol
        }
        return image
    }()

    private static var cache: [Look: NSImage] = [:]

    static func image(_ look: Look) -> NSImage {
        if let cached = cache[look] { return cached }
        let image = NSImage(size: size, flipped: false) { rect in
            base.draw(in: NSRect(x: 0, y: 0, width: 18, height: 18), from: .zero, operation: .sourceOver,
                      fraction: look == .stopped ? 0.4 : 1)
            if case .working(let hollow) = look {
                // A dot at the top right, clear of the M's right leg (over the leg it read as "Mi").
                let dot = NSRect(x: 18.2, y: 12.6, width: 3.8, height: 3.8)
                NSColor.black.set()
                if hollow {
                    let ring = NSBezierPath(ovalIn: dot.insetBy(dx: 0.5, dy: 0.5))
                    ring.lineWidth = 0.9
                    ring.stroke()
                } else {
                    NSBezierPath(ovalIn: dot).fill()
                }
            }
            return true
        }
        image.isTemplate = true
        cache[look] = image
        return image
    }
}
