import AppKit
import WebKit

/// Forwards page messages without the user content controller retaining the window controller.
private final class OnboardingMessageForwarder: NSObject, WKScriptMessageHandler {
    weak var target: OnboardingWindowController?

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        let body = message.body
        Task { @MainActor [weak target] in target?.receive(body) }
    }
}

/// The first-run tour and setup (menu_app/Onboarding), shown in a web view so it can carry
/// motion and color. The page can only ask for the actions OnboardingBridge allows.
@MainActor
final class OnboardingWindowController: NSObject, NSWindowDelegate, WKNavigationDelegate {
    static let shared = OnboardingWindowController()

    private var window: NSWindow?
    private var webView: WKWebView?
    private weak var state: AppState?
    private var finished = false

    var isShown: Bool { window?.isVisible ?? false }

    func show(state: AppState) {
        self.state = state
        if let window {
            NSApp.activate(ignoringOtherApps: true)
            window.makeKeyAndOrderFront(nil)
            return
        }
        guard let page = Bundle.main.url(forResource: "index", withExtension: "html", subdirectory: "Onboarding") else {
            // Built without the page: Settings › Get Started covers the same steps.
            SettingsWindowController.shared.show(state: state, settings: state.settings, section: .start)
            return
        }
        finished = false
        let forwarder = OnboardingMessageForwarder()
        forwarder.target = self
        let configuration = WKWebViewConfiguration()
        configuration.userContentController.add(forwarder, name: "macmcp")
        let web = WKWebView(frame: NSRect(x: 0, y: 0, width: 960, height: 640), configuration: configuration)
        web.navigationDelegate = self
        web.underPageBackgroundColor = NSColor(srgbRed: 0.082, green: 0.106, blue: 0.18, alpha: 1)
        // Resources/Onboarding and Resources/Assets (the icon) are both readable, nothing else.
        web.loadFileURL(page, allowingReadAccessTo: page.deletingLastPathComponent().deletingLastPathComponent())

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 960, height: 640),
            styleMask: [.titled, .closable, .miniaturizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Welcome to Mac MCP"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.backgroundColor = NSColor(srgbRed: 0.082, green: 0.106, blue: 0.18, alpha: 1)
        window.contentView = web
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.center()
        self.window = window
        self.webView = web
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    func close() {
        window?.close()
    }

    // Stay on the bundled page: no navigation anywhere else.
    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy) -> Void
    ) {
        decisionHandler(navigationAction.request.url?.isFileURL == true ? .allow : .cancel)
    }

    fileprivate func receive(_ body: Any) {
        guard let message = OnboardingBridge.parse(body), let state else { return }
        if case .finish = message.action { finished = true }
        Task { @MainActor in
            do {
                let result = try await perform(message.action, state: state)
                reply(message.id, ok: true, result: result, error: nil)
            } catch {
                reply(message.id, ok: false, result: nil, error: error.localizedDescription)
            }
            if case .finish = message.action { close() }
        }
    }

    /// What the page asked for (already checked by OnboardingBridge).
    private func perform(_ action: OnboardingAction, state: AppState) async throws -> Any? {
        switch action {
        case .status:
            return await state.onboardingStatusPayload()
        case .startServer:
            guard await state.startServerForOnboarding() else {
                throw AppState.OnboardingFailure(message: "Mac MCP did not start. Open Settings › Help & Diagnostics to see why.")
            }
            return true
        case .restartServer:
            guard await state.restartServerForOnboarding() else {
                throw AppState.OnboardingFailure(message: "Mac MCP did not restart. Try Restart Server in the menu.")
            }
            return true
        case .agents:
            // Agents without an AI app of their own (Kimi Code, Grok Build, Antigravity, …) are found by the server.
            if state.providerStatuses.isEmpty { await state.refreshProviderDetection() }
            return Self.agentStates(state)
        case .enableAgent(let provider), .disableAgent(let provider):
            // Switched here by the owner, like the switch in Settings › Agents (never on by default).
            let enabled = action == .enableAgent(provider)
            let settings = state.settings
            guard !settings.providerSettingsLocked else {
                throw AppState.OnboardingFailure(message: "Mac MCP's settings file could not be read, so agents stay as they are. Open Settings › Agents.")
            }
            let previous = settings.providerEnabled(provider)
            settings.setProviderEnabled(provider, enabled: enabled)
            do {
                try settings.save()
            } catch {
                settings.setProviderEnabled(provider, enabled: previous)
                throw AppState.OnboardingFailure(message: "Could not save the setting: \(error.localizedDescription)")
            }
            return Self.agentStates(state)
        case .requestPermission(let permission):
            return try await state.dashboardJSON("dashboard/api/permissions/request", body: ["permission": permission])
        case .openSystemSettings(let permission):
            if let url = OnboardingBridge.systemSettingsURL(permission) { NSWorkspace.shared.open(url) }
            return true
        case .connectConfig(let client):
            let result = await state.runCLICommand(["connect-config", "--client", client])
            let text = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
            if result.code != 0 {
                throw AppState.OnboardingFailure(message: text.isEmpty ? "This app's setup is not available yet." : text)
            }
            return text
        case .apps:
            let result = await state.runCLICommand(["clients", "--json"])
            guard let object = OnboardingBridge.jsonResult(result.output), let apps = object["apps"] else {
                throw AppState.OnboardingFailure(message: "Mac MCP could not look for AI apps on this Mac.")
            }
            return apps
        case .connectApp(let client):
            // Mac MCP writes the app's setup itself; for ChatGPT the address goes to the clipboard.
            let result = await state.runCLICommand(["connect", "--client", client, "--json"])
            guard let object = OnboardingBridge.jsonResult(result.output) else {
                throw AppState.OnboardingFailure(message: "Mac MCP could not set up this app. Use the manual setup below.")
            }
            if object["ok"] as? Bool != true {
                throw AppState.OnboardingFailure(message: object["error"] as? String ?? "Mac MCP could not set up this app.")
            }
            return object
        case .connectionInfo:
            let result = await state.runCLICommand(["connect", "--client", "other", "--json"])
            guard let object = OnboardingBridge.jsonResult(result.output) else {
                throw AppState.OnboardingFailure(message: "Mac MCP could not read this Mac's setup.")
            }
            if object["ok"] as? Bool != true {
                throw AppState.OnboardingFailure(message: object["error"] as? String ?? "Mac MCP could not read this Mac's setup.")
            }
            return object
        case .copyKey:
            // The CLI puts the key on the clipboard itself; it never passes through this page.
            let result = await state.runCLICommand(["connect", "--client", "other", "--copy-key", "--json"])
            guard let object = OnboardingBridge.jsonResult(result.output), object["ok"] as? Bool == true else {
                let reason = OnboardingBridge.jsonResult(result.output)?["error"] as? String
                throw AppState.OnboardingFailure(message: reason ?? "Mac MCP could not copy its key.")
            }
            return true
        case .openChatGPT:
            NSWorkspace.shared.open(OnboardingBridge.chatGPTSettingsURL)
            return true
        case .copy(let text):
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
            return true
        case .openSettings(let section):
            SettingsWindowController.shared.show(state: state, settings: state.settings, section: SettingsSection(rawValue: section))
            return true
        case .finish(let outcome):
            state.recordTour(outcome)
            return true
        case .setupBrowser(let browser):
            // The same setup as Settings › Connections › Browser; the page polls status to see it finish.
            if browser == "safari" { state.openSafariExtensionPreferences() } else { state.openChromeExtensionSetup() }
            return true
        }
    }

    /// Which agents are on (Settings › Agents) and which the server found on this Mac,
    /// for the tour's agent row and its list.
    private static func agentStates(_ state: AppState) -> [String: [String: Bool]] {
        let providers = OnboardingBridge.agentProviders
        let enabled = Dictionary(uniqueKeysWithValues: providers.map { ($0, state.settings.providerEnabled($0)) })
        let installed = Dictionary(uniqueKeysWithValues: providers.map { id in
            (id, state.providerStatuses.first(where: { $0.id == id })?.detected == true)
        })
        return ["enabled": enabled, "installed": installed]
    }

    private func reply(_ id: Int, ok: Bool, result: Any?, error: String?) {
        webView?.evaluateJavaScript(OnboardingBridge.replyScript(id: id, ok: ok, result: result, error: error))
    }

    func windowWillClose(_ notification: Notification) {
        // Closing the window counts as skipping, so the tour does not open again on its own.
        if !finished { state?.recordTour("skipped") }
        webView?.configuration.userContentController.removeScriptMessageHandler(forName: "macmcp")
        webView = nil
        window = nil
    }
}
