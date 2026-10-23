import SwiftUI

/// How serious a status is. Views pair the tone's color with a symbol and text,
/// so color is never the only signal.
enum StatusTone: Equatable {
    case ok
    case warning
    case error
    case neutral

    var color: Color {
        switch self {
        case .ok: return VisualTokens.success
        case .warning: return VisualTokens.warning
        case .error: return VisualTokens.error
        case .neutral: return .secondary
        }
    }
}

/// One label, symbol and short explanation for a status, shared by the menu-bar
/// popover and Settings so both surfaces say the same thing.
struct StatusDisplay: Equatable {
    let label: String
    let symbol: String
    let explanation: String
    let tone: StatusTone
}

extension DashboardConnectionState {
    var display: StatusDisplay {
        switch self {
        case .connected:
            return StatusDisplay(label: "Connected", symbol: "checkmark.circle.fill",
                                 explanation: "Mac MCP is running, and your AI apps can use this Mac.", tone: .ok)
        case .degraded:
            return StatusDisplay(label: "Partly working", symbol: "exclamationmark.triangle.fill",
                                 explanation: "Mac MCP answers, but some of its information is missing.", tone: .warning)
        case .disconnected:
            return StatusDisplay(label: "Not connected", symbol: "xmark.circle.fill",
                                 explanation: "Mac MCP is not answering.", tone: .error)
        case .connecting:
            return StatusDisplay(label: "Connecting", symbol: "arrow.triangle.2.circlepath",
                                 explanation: "Checking Mac MCP.", tone: .neutral)
        }
    }
}

enum EndpointDisplay {
    /// Remote access in plain words, keeping what is set up apart from what was seen running.
    /// The running check is the local connection process; nothing here checks reachability.
    static func make(mode: String, cloudflareRunning: Bool, ngrokRunning: Bool) -> StatusDisplay {
        switch mode {
        case "cloudflare":
            return cloudflareRunning
                ? StatusDisplay(label: "On", symbol: "network",
                                explanation: "Remote access through Cloudflare is on, and its connection is running on this Mac.", tone: .ok)
                : StatusDisplay(label: "On, but stopped", symbol: "network.slash",
                                explanation: "Remote access through Cloudflare is set up, but its connection is not running on this Mac.", tone: .warning)
        case "ngrok":
            return ngrokRunning
                ? StatusDisplay(label: "On", symbol: "network",
                                explanation: "Remote access through ngrok is on, and its connection is running on this Mac.", tone: .ok)
                : StatusDisplay(label: "On, but stopped", symbol: "network.slash",
                                explanation: "Remote access through ngrok is set up, but its connection is not running on this Mac.", tone: .warning)
        case "custom":
            return StatusDisplay(label: "Your own address", symbol: "link",
                                 explanation: "Remote access goes through your own address. Mac MCP does not check whether it answers.", tone: .neutral)
        default:
            return StatusDisplay(label: "This Mac only", symbol: "lock.laptopcomputer",
                                 explanation: "Only AI apps on this Mac can connect.", tone: .neutral)
        }
    }

    /// Configured but not observed, so the user may want to look at Connections.
    static func needsReview(mode: String, cloudflareRunning: Bool, ngrokRunning: Bool) -> Bool {
        (mode == "cloudflare" && !cloudflareRunning) || (mode == "ngrok" && !ngrokRunning)
    }
}

enum SafetyDisplay {
    static func profileName(_ value: String) -> String {
        switch value.lowercased() {
        case "read_only": return "Read Only"
        case "standard": return "Standard"
        case "trusted": return "Trusted"
        default: return value.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    /// What a permission profile lets AI apps do, in plain words (#246).
    static func profileSummary(_ value: String) -> String? {
        switch value.lowercased() {
        case "trusted":
            return "AI apps can do everything Mac MCP offers: read and change files, run commands, use your apps and the browser, hand work to agents and update Mac MCP."
        case "standard":
            return "AI apps can read and change files, use your apps and the browser and hand work to agents, but cannot run commands directly or update Mac MCP. Agents they start can only read."
        case "read_only":
            return "AI apps can look and read (files, apps, the browser and the web) but cannot change anything."
        default: return nil
        }
    }

    /// When Mac MCP itself asks the owner before an action, in plain words (#246).
    static func approvalSummary(_ value: String) -> String? {
        switch value.lowercased() {
        case "off":
            return "Doesn't ask for routine work. It still asks where trust matters most, for example before a password or key would go to an unknown website."
        case "critical":
            return "Asks before running commands, updating Mac MCP or stopping programs."
        case "high_risk":
            return "Asks before all of the above, and before deleting or changing things in your apps, the browser or online."
        default: return nil
        }
    }

    /// The active permission profile. Amber when the server has not reported its
    /// semantics, or reports an unknown, invalid or substituted profile.
    static func make(_ semantics: SecuritySemanticsEnvelope?, serverRunning: Bool) -> StatusDisplay {
        guard let semantics else {
            return StatusDisplay(
                label: "Unavailable", symbol: "exclamationmark.shield",
                explanation: serverRunning ? "The server has not reported its permission profile." : "Start the server to read the permission profile.",
                tone: .warning
            )
        }
        let name = profileName(semantics.activeProfile)
        let profileListed = semantics.profiles.contains { $0.name == semantics.activeProfile }
        if !semantics.knownProfile || !profileListed {
            return StatusDisplay(label: "\(name) · unknown", symbol: "exclamationmark.shield",
                                 explanation: "Unknown profile. Calls fail closed until a known profile is set.", tone: .warning)
        }
        if semantics.profileWasNormalized == true {
            let configured = profileName(semantics.normalizedFromProfile ?? semantics.configuredProfile ?? "configured")
            return StatusDisplay(label: "\(name) · substituted", symbol: "exclamationmark.shield",
                                 explanation: "\(configured) is not valid here, so \(name) is used instead.", tone: .warning)
        }
        return StatusDisplay(label: name, symbol: "checkmark.shield",
                             explanation: "Active permission profile.", tone: .neutral)
    }
}
