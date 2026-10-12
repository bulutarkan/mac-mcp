import Foundation

// Get Started (#59): a short, local-first path from install to a first request that worked.
// Each step is checked from what the server has actually seen, so a returning owner whose
// setup already works never sees it. Foundation only, so the logic is tested on its own.

/// GET /dashboard/api/onboarding.
struct OnboardingStatus: Decodable, Equatable {
    struct Call: Decodable, Equatable {
        let at: Double
        let tool: String
        let source: String
        let status: String
    }

    let firstCall: Call?
    let firstSuccess: Call?
    /// granted, denied, not_determined or unknown, as the server process sees it.
    let accessibility: String
    let screenRecording: String
    let publicEndpointMode: String

    enum CodingKeys: String, CodingKey {
        case accessibility
        case firstCall = "first_call"
        case firstSuccess = "first_success"
        case screenRecording = "screen_recording"
        case publicEndpointMode = "public_endpoint_mode"
    }
}

enum OnboardingStepID: String {
    case server, client, firstRequest, permissions
}

struct OnboardingStep: Identifiable, Equatable {
    let id: OnboardingStepID
    let title: String
    let detail: String
    let done: Bool
}

struct OnboardingChecklist: Equatable {
    /// Read-only: it asks for system information and changes nothing.
    static let firstRequestPrompt = "Use Mac MCP to tell me which macOS version this Mac is running."

    let steps: [OnboardingStep]

    var completed: Int { steps.filter(\.done).count }
    var isComplete: Bool { completed == steps.count }
    var nextStep: OnboardingStep? { steps.first { !$0.done } }

    static func make(serverRunning: Bool, status: OnboardingStatus?) -> OnboardingChecklist {
        let known = serverRunning ? status : nil
        let remote = (known?.publicEndpointMode ?? "none") != "none"
        let permissionsGranted = known?.accessibility == "granted" && known?.screenRecording == "granted"
        let connectDetail = known?.firstCall != nil
            ? "An AI app has called Mac MCP."
            : "Add Mac MCP to Claude Code, Codex or OpenCode in Connections › Client Setup. They reach this Mac directly."
                + (remote ? "" : " ChatGPT needs a public HTTPS endpoint first; it is optional and off by default.")
        return OnboardingChecklist(steps: [
            OnboardingStep(
                id: .server,
                title: "Start Mac MCP",
                detail: serverRunning ? "The server is running on this Mac." : "Start the server so AI apps can reach it.",
                done: serverRunning
            ),
            OnboardingStep(id: .client, title: "Connect an AI app", detail: connectDetail, done: known?.firstCall != nil),
            OnboardingStep(
                id: .firstRequest,
                title: "Try a first request",
                detail: known?.firstSuccess != nil
                    ? "A request through your AI app worked."
                    : "Ask your AI app: “\(firstRequestPrompt)” It only reads; nothing on your Mac changes.",
                done: known?.firstSuccess != nil
            ),
            OnboardingStep(
                id: .permissions,
                title: "Allow Accessibility and Screen Recording",
                detail: permissionsGranted
                    ? "Agents can read and use your Mac apps."
                    : "Needed when agents read or use Mac apps. Allow Mac MCP in System Settings › Privacy & Security.",
                done: permissionsGranted
            ),
        ])
    }

    /// The menu shows a reminder only while setup is unfinished and the owner has not hidden it.
    static func shouldShowReminder(_ checklist: OnboardingChecklist, dismissed: Bool) -> Bool {
        !dismissed && !checklist.isComplete
    }
}
