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
    case server, client, firstRequest
    /// Optional (#247): only Mac app work needs them, so they never hold setup back.
    case appControl, screenCapture
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

    /// What setup needs: a running server, a connected AI app and a first request that worked.
    let steps: [OnboardingStep]
    /// Permissions for Mac app work; shown with the steps but never counted against setup.
    let optional: [OnboardingStep]

    var completed: Int { steps.filter(\.done).count }
    var isComplete: Bool { completed == steps.count }
    var nextStep: OnboardingStep? { steps.first { !$0.done } }

    static func make(serverRunning: Bool, status: OnboardingStatus?) -> OnboardingChecklist {
        let known = serverRunning ? status : nil
        let remote = (known?.publicEndpointMode ?? "none") != "none"
        let appControl = known?.accessibility == "granted"
        let screenCapture = known?.screenRecording == "granted"
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
        ], optional: [
            OnboardingStep(
                id: .appControl,
                title: "Let AI apps use your Mac apps",
                detail: appControl
                    ? "Accessibility is on: AI apps can click, type and read in Mac app windows."
                    : "Accessibility, only for clicking, typing and reading in Mac apps such as Notes or Mail. Files, the terminal and the browser work without it.",
                done: appControl
            ),
            OnboardingStep(
                id: .screenCapture,
                title: "Let AI apps see a window",
                detail: screenCapture
                    ? "Screen Recording is on: AI apps can look at a window when its buttons are not enough."
                    : "Screen Recording, only for looking at a window when an app’s buttons are not enough.",
                done: screenCapture
            ),
        ])
    }

    /// The menu shows a reminder only while setup is unfinished and the owner has not hidden it.
    static func shouldShowReminder(_ checklist: OnboardingChecklist, dismissed: Bool) -> Bool {
        !dismissed && !checklist.isComplete
    }
}
