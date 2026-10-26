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
        OwnerPromptController.shared.attach(appState)
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
            MenuBarView(state: state, settings: state.settings, prompts: OwnerPromptController.shared.model)
        } label: {
            MenuBarLabel(state: state, prompts: OwnerPromptController.shared.model)
        }
        .menuBarExtraStyle(.window)
        .commands {
            CommandGroup(after: .newItem) {
                Button("Mac MCP Chat") { ChatWindowController.shared.show(state: state) }
                    .keyboardShortcut("n", modifiers: [.command, .shift])
            }
            CommandGroup(replacing: .appSettings) {
                Button("Settings…") {
                    SettingsWindowController.shared.show(state: state, settings: state.settings)
                }
                .keyboardShortcut(",", modifiers: .command)
            }
        }
    }

}

/// The menu bar item. It also watches the question window's queue, so "needs you" shows here too.
private struct MenuBarLabel: View {
    @ObservedObject var state: AppState
    @ObservedObject var prompts: OwnerPromptModel

    var body: some View {
        Image(nsImage: MenuBarMark.image(menuBarLook))
            .accessibilityLabel(menuBarAccessibilityLabel)
    }

    /// The same live state the popup header says (MenuLiveStatus, #123).
    private var live: MenuLiveStatus {
        state.liveStatus(questionsWaiting: prompts.queue.count, askedBy: prompts.queue.first?.sender)
    }

    /// One look at a time, most important first: stopped, a question waiting, paused, working, idle.
    /// Not running yet (connecting, starting) keeps the dimmed mark: nothing can use it yet.
    private var menuBarLook: MenuBarMark.Look {
        switch live.iconLook {
        case .stopped: return .stopped
        case .needsYou: return .needsYou
        case .paused: return .paused
        // The dot stays filled without the pulse (Reduce Motion), so work still reads at a glance.
        case .working: return .working(hollow: state.pulse)
        case .idle: return .idle
        }
    }

    private var menuBarAccessibilityLabel: String {
        // Same words as the popover header and Settings (shared StatusDisplay mapping).
        let live = live
        if live.server == .starting { return "Mac MCP, starting" }
        if live.server != .running { return "Mac MCP, \(state.connectionState.display.label.lowercased())" }
        var parts = ["Mac MCP"]
        if live.questionsWaiting > 0 { parts.append("needs you") }
        if live.paused { parts.append("observation paused") }
        if live.isWorking {
            parts.append("active work")
        }
        if state.privacyFlags?.privateMode == true { parts.append("private mode") }
        if state.connectionState == .degraded { parts.append("partly working") }
        return parts.joined(separator: ", ")
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
/// and highlighted bars; the state shows in alpha and a glyph right of the M, never in colour.
@MainActor
enum MenuBarMark {
    enum Look: Hashable {
        case stopped, idle, needsYou, paused
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
            NSColor.black.set()
            switch look {
            case .needsYou:
                // An exclamation mark beside the M: a question is waiting in Mac MCP's window.
                NSBezierPath(roundedRect: NSRect(x: 19.2, y: 8.4, width: 2.2, height: 8), xRadius: 1.1, yRadius: 1.1).fill()
                NSBezierPath(ovalIn: NSRect(x: 19.2, y: 4.2, width: 2.2, height: 2.2)).fill()
            case .paused:
                // Pause bars beside the M: observation is paused.
                for x in [18.4, 20.8] {
                    NSBezierPath(roundedRect: NSRect(x: x, y: 9.6, width: 1.4, height: 6.4), xRadius: 0.5, yRadius: 0.5).fill()
                }
            case .working(let hollow):
                // A dot at the top right, clear of the M's right leg (over the leg it read as "Mi").
                let dot = NSRect(x: 18.2, y: 12.6, width: 3.8, height: 3.8)
                if hollow {
                    let ring = NSBezierPath(ovalIn: dot.insetBy(dx: 0.5, dy: 0.5))
                    ring.lineWidth = 0.9
                    ring.stroke()
                } else {
                    NSBezierPath(ovalIn: dot).fill()
                }
            case .stopped, .idle:
                break
            }
            return true
        }
        image.isTemplate = true
        cache[look] = image
        return image
    }
}
