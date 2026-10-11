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
        case .ok: return .green
        case .warning: return .orange
        case .error: return .red
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
                                 explanation: "The local server is running and answering.", tone: .ok)
        case .degraded:
            return StatusDisplay(label: "Degraded", symbol: "exclamationmark.triangle.fill",
                                 explanation: "The server answers, but some data is unavailable.", tone: .warning)
        case .disconnected:
            return StatusDisplay(label: "Disconnected", symbol: "xmark.circle.fill",
                                 explanation: "The local server is not answering.", tone: .error)
        case .connecting:
            return StatusDisplay(label: "Connecting", symbol: "arrow.triangle.2.circlepath",
                                 explanation: "Checking the local server.", tone: .neutral)
        }
    }
}

enum EndpointDisplay {
    /// Public endpoint state that keeps what is configured apart from what was observed.
    /// "Detected" only reflects the local process check; nothing here checks reachability.
    static func make(mode: String, cloudflareRunning: Bool, ngrokRunning: Bool) -> StatusDisplay {
        switch mode {
        case "cloudflare":
            return cloudflareRunning
                ? StatusDisplay(label: "Cloudflare · tunnel process detected", symbol: "network",
                                explanation: "Remote access is configured and the tunnel process is running on this Mac.", tone: .ok)
                : StatusDisplay(label: "Cloudflare · tunnel not detected", symbol: "network.slash",
                                explanation: "Remote access is configured, but no tunnel process was found on this Mac.", tone: .warning)
        case "ngrok":
            return ngrokRunning
                ? StatusDisplay(label: "ngrok · tunnel process detected", symbol: "network",
                                explanation: "Remote access is configured and the ngrok process is running on this Mac.", tone: .ok)
                : StatusDisplay(label: "ngrok · tunnel not detected", symbol: "network.slash",
                                explanation: "Remote access is configured, but no ngrok process was found on this Mac.", tone: .warning)
        case "custom":
            return StatusDisplay(label: "Custom URL configured · not checked", symbol: "link",
                                 explanation: "Mac MCP does not check whether this URL answers.", tone: .neutral)
        default:
            return StatusDisplay(label: "Local only", symbol: "lock.laptopcomputer",
                                 explanation: "Only apps on this Mac can connect.", tone: .neutral)
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
