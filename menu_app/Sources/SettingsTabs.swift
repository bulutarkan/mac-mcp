import SwiftUI

/// Settings panes that hold several separate subjects show them in tabs instead of one long page:
/// Agents, Usage, Privacy and Apps. Each pane keeps the tab last shown, and a macmcp://settings/<name> link
/// can open one tab, such as macmcp://settings/skills.
protocol SettingsTab: CaseIterable, Identifiable, Hashable, RawRepresentable
where RawValue == String, AllCases: RandomAccessCollection {
    var title: String { get }
    var symbol: String { get }
}

extension SettingsTab {
    var id: String { rawValue }
}

enum AgentsTab: String, SettingsTab {
    case general
    case providers
    case skills
    case history

    var title: String {
        switch self {
        case .general: return "General"
        case .providers: return "Providers"
        case .skills: return "Skills"
        case .history: return "Team History"
        }
    }

    var symbol: String {
        switch self {
        case .general: return "slider.horizontal.3"
        case .providers: return "shippingbox"
        case .skills: return "book.closed"
        case .history: return "clock.arrow.circlepath"
        }
    }
}

enum UsageTab: String, SettingsTab {
    case tools
    case agents
    case chat
    case data

    var title: String {
        switch self {
        case .tools: return "Tools"
        case .agents: return "Delegated Agents"
        case .chat: return "Chat"
        case .data: return "Data & Storage"
        }
    }

    var symbol: String {
        switch self {
        case .tools: return "wrench.and.screwdriver"
        case .agents: return "cpu"
        case .chat: return "bubble.left.and.text.bubble.right"
        case .data: return "externaldrive"
        }
    }
}

enum PrivacyTab: String, SettingsTab {
    case protection
    case documents
    case data

    var title: String {
        switch self {
        case .protection: return "Protection"
        case .documents: return "Documents"
        case .data: return "Your Data"
        }
    }

    var symbol: String {
        switch self {
        case .protection: return "hand.raised"
        case .documents: return "doc.text.magnifyingglass"
        case .data: return "externaldrive"
        }
    }
}

enum AppsTab: String, SettingsTab {
    case sending
    case shortcuts
    case support

    var title: String {
        switch self {
        case .sending: return "Mail & Messages"
        case .shortcuts: return "Shortcuts"
        case .support: return "App Support"
        }
    }

    var symbol: String {
        switch self {
        case .sending: return "paperplane"
        case .shortcuts: return "square.stack.3d.up"
        case .support: return "square.grid.2x2"
        }
    }
}

/// The tab each pane shows; kept between launches.
@MainActor
final class SettingsTabs: ObservableObject {
    static let shared = SettingsTabs()

    @Published var agents: AgentsTab { didSet { Self.keep(agents, for: "agents") } }
    @Published var usage: UsageTab { didSet { Self.keep(usage, for: "usage") } }
    @Published var privacy: PrivacyTab { didSet { Self.keep(privacy, for: "privacy") } }
    @Published var apps: AppsTab { didSet { Self.keep(apps, for: "apps") } }

    private init() {
        agents = Self.kept(for: "agents") ?? .general
        usage = Self.kept(for: "usage") ?? .tools
        privacy = Self.kept(for: "privacy") ?? .protection
        apps = Self.kept(for: "apps") ?? .sending
    }

    /// Links that name a tab, and the pane each one opens.
    private static let links: [String: SettingsSection] = [
        "providers": .agents, "skills": .agents, "team-history": .agents,
        "tool-usage": .usage, "agent-usage": .usage, "chat-usage": .usage, "usage-data": .usage,
        "documents": .privacy, "knowledge": .privacy, "your-data": .privacy,
        "sending": .apps, "shortcuts": .apps, "app-support": .apps,
    ]

    nonisolated static func section(forLink link: String) -> SettingsSection? { links[link] }

    /// Shows the tab a link names; any other link leaves every pane on its last tab.
    func select(link: String) {
        switch link {
        case "providers": agents = .providers
        case "skills": agents = .skills
        case "team-history": agents = .history
        case "tool-usage": usage = .tools
        case "agent-usage": usage = .agents
        case "chat-usage": usage = .chat
        case "usage-data": usage = .data
        case "documents", "knowledge": privacy = .documents
        case "your-data": privacy = .data
        case "sending": apps = .sending
        case "shortcuts": apps = .shortcuts
        case "app-support": apps = .support
        default: break
        }
    }

    private static func key(_ pane: String) -> String { "settings.\(pane).tab" }

    private static func kept<Tab: SettingsTab>(for pane: String) -> Tab? {
        Tab(rawValue: UserDefaults.standard.string(forKey: key(pane)) ?? "")
    }

    private static func keep<Tab: SettingsTab>(_ tab: Tab, for pane: String) {
        UserDefaults.standard.set(tab.rawValue, forKey: key(pane))
    }
}

/// The segmented control above a tabbed pane.
struct SettingsTabPicker<Tab: SettingsTab>: View {
    @Binding var selection: Tab
    let label: String

    var body: some View {
        Picker("", selection: $selection) {
            ForEach(Tab.allCases) { tab in
                Label(tab.title, systemImage: tab.symbol).tag(tab)
            }
        }
        .labelsHidden()
        .pickerStyle(.segmented)
        .fixedSize()
        .accessibilityLabel(label)
    }
}
