import Foundation

/// Settings › Agents in four tabs, so the default agent, the providers, the skills list and team history
/// each get their own screen instead of one long page.
enum AgentsTab: String, CaseIterable, Identifiable {
    case general
    case providers
    case skills
    case history

    var id: String { rawValue }

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

    /// A macmcp://settings/<name> link that opens one tab, such as macmcp://settings/skills.
    init?(link: String) {
        switch link {
        case "providers": self = .providers
        case "skills": self = .skills
        case "team-history", "history": self = .history
        default: return nil
        }
    }
}

/// Which Agents tab is shown; the last one is kept for the next time Settings opens.
@MainActor
final class AgentsNavigation: ObservableObject {
    static let shared = AgentsNavigation()
    private static let key = "settings.agents.tab"

    @Published var tab: AgentsTab {
        didSet { UserDefaults.standard.set(tab.rawValue, forKey: Self.key) }
    }

    private init() {
        tab = AgentsTab(rawValue: UserDefaults.standard.string(forKey: Self.key) ?? "") ?? .general
    }
}
